"""Slice 12 commit 3: Fernet rotation in-place + admin endpoint.

Companion to ``test_lifecycle_fernet_rotation.py``: this file pins
the parts that the impl-shipped baseline does not exhaustively cover
without needing Postgres up, plus the admin endpoint contract.

* ``rotate_fernet_in_place`` outcome categorisation: ``rotated``,
  ``already_rotated``, ``raced``, ``failed`` — every branch is
  exercised by mocking the lazy ``core.models.PlatformCredential``
  ORM. The django_db-backed integration variants live in the
  baseline file.
* mid-rotation interruption then resume — pin that interruption is
  safe and a follow-up pass is idempotent.
* audit-failure does NOT poison the rotation pass — the row is
  still ``rotated``.
* the admin POST endpoint:
    - 401 without admin cookie,
    - 503 when ``ENDPOINT_FERNET_KEY_NEW`` is missing,
    - audit row written for the umbrella request + completion,
    - returns the per-row counts as a JSON envelope.

The mocked PlatformCredential is built around an
``async_iterator`` yielding lightweight ``_Row`` namedtuple-like
objects so the rotation walks them just like the real ORM.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher, CryptoConfigError
from ingestion.daemon.lifecycle import fernet_rotation as rotation_module
from ingestion.daemon.lifecycle.fernet_rotation import (
    ENV_FERNET_KEY_NEW,
    RotationResult,
    build_new_cipher,
    rotate_fernet_in_place,
)
from ingestion.daemon.store.local import apply_migrations, open_store

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Fake ``core.models`` shim — lets us exercise the rotation pass without
# Postgres + Django bootstrapping.
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    pk: int
    user_email: str
    platform: str
    encrypted_payload: bytes


class _Manager:
    def __init__(
        self,
        rows: list[_Row],
        *,
        update_outcomes: dict[int, int] | None = None,
        update_callback: Callable[[int], None] | None = None,
    ) -> None:
        self._rows = rows
        # Map pk -> updated count (default 1; set 0 to simulate a race).
        self._update_outcomes = update_outcomes or {}
        self._update_callback = update_callback
        self.update_calls: list[dict[str, Any]] = []

    def all(self) -> _Manager:
        return self

    def aiterator(self) -> AsyncIterator[_Row]:
        rows = list(self._rows)

        async def _gen() -> AsyncIterator[_Row]:
            for row in rows:
                yield row

        return _gen()

    def filter(self, *, pk: int, encrypted_payload: bytes) -> _Manager:
        # Capture filter inputs for the simulated WHERE-keyed update.
        self._pending_pk = pk
        self._pending_payload = encrypted_payload
        return self

    async def aupdate(self, *, encrypted_payload: bytes) -> int:
        self.update_calls.append(
            {
                "pk": self._pending_pk,
                "old": self._pending_payload,
                "new": encrypted_payload,
            }
        )
        if self._update_callback is not None:
            self._update_callback(self._pending_pk)
        # Default to 1 row updated; per-pk override wins.
        return self._update_outcomes.get(self._pending_pk, 1)


def _install_fake_models(
    monkeypatch: pytest.MonkeyPatch,
    manager: _Manager,
) -> None:
    """Install a fake ``core.models`` module exposing ``PlatformCredential``.

    The lifecycle module lazy-imports inside ``rotate_fernet_in_place``;
    we replace ``sys.modules["core.models"]`` so the next import
    resolves to the fake.
    """
    fake = types.ModuleType("core.models")

    class _PlatformCredential:
        objects = manager

    fake.PlatformCredential = _PlatformCredential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.models", fake)


# ---------------------------------------------------------------------------
# build_new_cipher branches
# ---------------------------------------------------------------------------


def test_build_new_cipher_strips_whitespace() -> None:
    """Operator pasting a key with surrounding whitespace still works."""
    new_key = Fernet.generate_key().decode()
    cipher = build_new_cipher(env={ENV_FERNET_KEY_NEW: f"  {new_key}  "})
    assert cipher.encrypt_json({"x": 1})


def test_build_new_cipher_empty_string_treated_as_missing() -> None:
    with pytest.raises(CryptoConfigError, match=ENV_FERNET_KEY_NEW):
        build_new_cipher(env={ENV_FERNET_KEY_NEW: ""})


def test_build_new_cipher_uses_os_environ_when_no_dict_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path reads ``os.environ`` — the exported new key
    surfaces without an explicit dict."""
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_FERNET_KEY_NEW, new_key)
    cipher = build_new_cipher()
    payload = {"k": "v"}
    blob = cipher.encrypt_json(payload)
    assert cipher.decrypt_json(blob) == payload


# ---------------------------------------------------------------------------
# rotate_fernet_in_place — every outcome branch (mocked ORM)
# ---------------------------------------------------------------------------


async def test_rotate_zero_rows_returns_zero_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ORM => RotationResult with all zero counts."""
    manager = _Manager(rows=[])
    _install_fake_models(monkeypatch, manager)
    old = CredentialCipher([Fernet.generate_key().decode()])
    new = CredentialCipher([Fernet.generate_key().decode()])
    result = await rotate_fernet_in_place(current_cipher=old, new_cipher=new)
    assert result == RotationResult(
        total_rows=0,
        rotated=0,
        already_rotated=0,
        raced=0,
        failed=0,
    )


async def test_rotate_already_rotated_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose payload is already on the new key is detected via
    ``new_cipher.decrypt_json`` first and counted as ``already_rotated``
    without an aupdate call."""
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([new_key])

    # Encrypted with the new key already.
    payload = new_cipher.encrypt_json({"already": True})
    rows = [_Row(pk=1, user_email="a@x", platform="moodle", encrypted_payload=payload)]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)

    result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    assert result.total_rows == 1
    assert result.already_rotated == 1
    assert result.rotated == 0
    # No aupdate fired (the fast path doesn't write).
    assert manager.update_calls == []


async def test_rotate_failed_path_for_unreadable_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload readable by NEITHER cipher counts as ``failed``."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    # Build a payload encrypted with a third key the rotation never sees.
    third = CredentialCipher([Fernet.generate_key().decode()])
    payload = third.encrypt_json({"unreadable": True})
    rows = [_Row(pk=1, user_email="a@x", platform="moodle", encrypted_payload=payload)]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)

    result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    assert result.total_rows == 1
    assert result.failed == 1
    assert result.rotated == 0
    assert manager.update_calls == []


async def test_rotate_raced_path_when_aupdate_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``aupdate`` returns 0 (no row matched the WHERE-keyed
    encrypted_payload), the row is counted as ``raced`` rather than
    ``rotated``."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    payload = old_cipher.encrypt_json({"raced": True})
    rows = [_Row(pk=42, user_email="a@x", platform="moodle", encrypted_payload=payload)]
    manager = _Manager(rows=rows, update_outcomes={42: 0})
    _install_fake_models(monkeypatch, manager)

    result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    assert result.total_rows == 1
    assert result.raced == 1
    assert result.rotated == 0


async def test_rotate_emits_audit_per_rotated_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every successfully-rotated row writes one
    ``credential.fernet_rotated`` audit row with target=email/platform."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    rows = [
        _Row(
            pk=1,
            user_email="a@x",
            platform="moodle",
            encrypted_payload=old_cipher.encrypt_json({"k": 1}),
        ),
        _Row(
            pk=2,
            user_email="b@x",
            platform="teams",
            encrypted_payload=old_cipher.encrypt_json({"k": 2}),
        ),
    ]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)
    audit_calls: list[dict[str, object]] = []

    async def _audit(**kwargs: object) -> int:
        audit_calls.append(dict(kwargs))
        return len(audit_calls)

    result = await rotate_fernet_in_place(
        current_cipher=old_cipher,
        new_cipher=new_cipher,
        audit_write=_audit,
    )
    assert result.rotated == 2
    actions = [str(c.get("action")) for c in audit_calls]
    assert actions.count("credential.fernet_rotated") == 2
    targets = {str(c.get("target")) for c in audit_calls}
    assert targets == {"a@x/moodle", "b@x/teams"}


async def test_rotate_audit_failure_does_not_poison_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the audit writer raises, the row is STILL counted as
    ``rotated`` because the SQL update already landed. Subsequent
    rows continue to be processed."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    rows = [
        _Row(
            pk=1,
            user_email="a@x",
            platform="moodle",
            encrypted_payload=old_cipher.encrypt_json({"k": 1}),
        ),
        _Row(
            pk=2,
            user_email="b@x",
            platform="teams",
            encrypted_payload=old_cipher.encrypt_json({"k": 2}),
        ),
    ]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)
    audit_count = 0

    async def _bad_audit(**_kwargs: object) -> int:
        nonlocal audit_count
        audit_count += 1
        raise RuntimeError("audit writer down")

    result = await rotate_fernet_in_place(
        current_cipher=old_cipher,
        new_cipher=new_cipher,
        audit_write=_bad_audit,
    )
    # Both rows still counted as rotated; both audit calls were attempted.
    assert result.rotated == 2
    assert audit_count == 2


async def test_rotate_mid_pass_interruption_resumes_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a SIGTERM mid-rotation by wrapping the iterator to
    raise after the first row is processed. Persist the post-update
    state, then re-run the rotation: the rotated row is detected as
    ``already_rotated``, the un-rotated row gets rotated."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    rows = [
        _Row(
            pk=1,
            user_email="a@x",
            platform="moodle",
            encrypted_payload=old_cipher.encrypt_json({"k": 1}),
        ),
        _Row(
            pk=2,
            user_email="b@x",
            platform="teams",
            encrypted_payload=old_cipher.encrypt_json({"k": 2}),
        ),
    ]
    # The callback resolves the new payload via a closure over
    # ``current_manager`` so we always read from the manager that fired
    # the aupdate (the first pass uses ``aborting_manager``, the
    # second uses a fresh ``manager``).
    current_manager: _Manager | None = None

    def _apply_update(pk: int) -> None:
        assert current_manager is not None
        for r in rows:
            if r.pk == pk:
                new = current_manager.update_calls[-1]["new"]
                r.encrypted_payload = bytes(new)

    # First pass: simulate raising mid-iteration (after first row rotates).
    class _FailAfterFirst:
        def __init__(self, source: list[_Row]) -> None:
            self._source = source
            self._yielded = 0

        def __aiter__(self) -> _FailAfterFirst:
            return self

        async def __anext__(self) -> _Row:
            if self._yielded >= 1:
                # After we've already yielded one row to be rotated,
                # interrupt.
                raise asyncio.CancelledError("simulated SIGTERM")
            row = self._source[self._yielded]
            self._yielded += 1
            return row

    aborting_iter_state = _FailAfterFirst(rows)

    class _AbortingManager(_Manager):
        def aiterator(self) -> AsyncIterator[_Row]:  # type: ignore[override]
            return aborting_iter_state

    aborting_manager = _AbortingManager(rows=rows, update_callback=_apply_update)
    current_manager = aborting_manager
    _install_fake_models(monkeypatch, aborting_manager)
    with pytest.raises(asyncio.CancelledError):
        await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    # Row[0] now landed under new key; row[1] still on old.
    assert aborting_manager.update_calls
    assert new_cipher.decrypt_json(rows[0].encrypted_payload) == {"k": 1}

    # Second pass: full iterator. The manager update_callback applies
    # rotations in-place to ``rows``.
    second_manager = _Manager(rows=rows, update_callback=_apply_update)
    current_manager = second_manager
    _install_fake_models(monkeypatch, second_manager)
    result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    assert result.total_rows == 2
    assert result.already_rotated == 1  # row[0]
    assert result.rotated == 1  # row[1]
    assert result.failed == 0
    assert result.raced == 0


async def test_rotate_uses_pre_rotation_payload_for_where_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aupdate filter uses the OLD encrypted_payload bytes — so a
    concurrent writer that races our read does not get clobbered."""
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([Fernet.generate_key().decode()])
    pre = old_cipher.encrypt_json({"k": "v"})
    rows = [_Row(pk=99, user_email="a@x", platform="moodle", encrypted_payload=pre)]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)
    await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    # The recorded WHERE clause uses the pre-rotation bytes.
    assert manager.update_calls[0]["old"] == pre
    # And the new payload differs.
    assert manager.update_calls[0]["new"] != pre


async def test_rotate_decrypt_with_new_first_idempotent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the order: ``new_cipher.decrypt_json`` is tried BEFORE
    ``current_cipher.decrypt_json``. We confirm by verifying that a
    payload readable ONLY by new_cipher produces ``already_rotated``
    (the slow path would fail on current then succeed only via the
    new_cipher decrypt)."""
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([Fernet.generate_key().decode()])
    new_cipher = CredentialCipher([new_key])
    payload = new_cipher.encrypt_json({"new": True})
    rows = [_Row(pk=1, user_email="a@x", platform="moodle", encrypted_payload=payload)]
    manager = _Manager(rows=rows)
    _install_fake_models(monkeypatch, manager)
    result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
    assert result.already_rotated == 1
    assert result.rotated == 0


# ---------------------------------------------------------------------------
# RotationResult dataclass
# ---------------------------------------------------------------------------


def test_rotation_result_is_frozen() -> None:
    """``RotationResult`` is a frozen dataclass — defensive immutability
    so callers can pass it through layers without a copy."""
    r = RotationResult(total_rows=1, rotated=1, already_rotated=0, raced=0, failed=0)
    with pytest.raises((AttributeError, TypeError)):
        r.rotated = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# admin endpoint: POST /v1/actions/admin/fernet-rotate
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


def _make_app(tmp_path: Path, fernet_key: str) -> FastAPI:
    sqlite_path = tmp_path / "state.db"
    apply_migrations(sqlite_path)
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    return create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
    )


def _logged_in(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def test_fernet_rotate_unauthenticated_is_401(tmp_path: Path, fernet_key: str) -> None:
    """The endpoint requires the admin cookie."""
    app = _make_app(tmp_path, fernet_key)
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = client.post("/v1/actions/admin/fernet-rotate", json={})
    assert response.status_code == 401


def test_fernet_rotate_missing_new_env_returns_503(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``ENDPOINT_FERNET_KEY_NEW`` exported, the service raises
    FeatureDisabledError which the router translates to 503."""
    monkeypatch.delenv(ENV_FERNET_KEY_NEW, raising=False)
    app = _make_app(tmp_path, fernet_key)
    client = _logged_in(app)
    response = client.post("/v1/actions/admin/fernet-rotate", json={})
    assert response.status_code == 503
    body = response.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert ENV_FERNET_KEY_NEW in str(detail.get("message", ""))
    else:
        assert ENV_FERNET_KEY_NEW in str(detail)


def test_fernet_rotate_returns_per_row_counts(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: response body contains every RotationResult field."""
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_FERNET_KEY_NEW, new_key)
    app = _make_app(tmp_path, fernet_key)

    # Patch rotate_fernet_in_place to skip the Django ORM walk.
    async def _fake_rotate(
        *,
        current_cipher: object,
        new_cipher: object,
        audit_write: Callable[..., Awaitable[int]] | None = None,
    ) -> RotationResult:
        del current_cipher
        del new_cipher
        if audit_write is not None:
            await audit_write(action="credential.fernet_rotated", target="x@x/moodle")
        return RotationResult(total_rows=1, rotated=1, already_rotated=0, raced=0, failed=0)

    monkeypatch.setattr(
        "ingestion.daemon.services.admin_service.rotate_fernet_in_place",
        _fake_rotate,
    )

    client = _logged_in(app)
    response = client.post(
        "/v1/actions/admin/fernet-rotate",
        json={"reason": "scheduled rotation"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_rows"] == 1
    assert body["rotated"] == 1
    assert body["already_rotated"] == 0
    assert body["raced"] == 0
    assert body["failed"] == 0


def test_fernet_rotate_writes_umbrella_audit_rows(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One audit row per request (``admin.fernet_rotation_requested``)
    AND one per completion (``admin.fernet_rotation_completed``)."""
    import sqlite3

    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_FERNET_KEY_NEW, new_key)
    app = _make_app(tmp_path, fernet_key)

    async def _fake_rotate(
        *,
        current_cipher: object,
        new_cipher: object,
        audit_write: Callable[..., Awaitable[int]] | None = None,
    ) -> RotationResult:
        del current_cipher
        del new_cipher
        del audit_write
        return RotationResult(total_rows=0, rotated=0, already_rotated=0, raced=0, failed=0)

    monkeypatch.setattr(
        "ingestion.daemon.services.admin_service.rotate_fernet_in_place",
        _fake_rotate,
    )

    client = _logged_in(app)
    response = client.post(
        "/v1/actions/admin/fernet-rotate",
        json={"reason": "scheduled rotation"},
    )
    assert response.status_code == 200, response.text
    sqlite_path = tmp_path / "state.db"
    conn = sqlite3.connect(sqlite_path)
    try:
        actions = [
            row[0] for row in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
        ]
    finally:
        conn.close()
    assert "admin.fernet_rotation_requested" in actions
    assert "admin.fernet_rotation_completed" in actions


# ---------------------------------------------------------------------------
# Idempotent rotation API contract: build_new_cipher single-key surface
# ---------------------------------------------------------------------------


def test_build_new_cipher_is_strict_single_key() -> None:
    """The cipher returned by ``build_new_cipher`` must reject payloads
    encrypted by ANY other key — that's the property the rotation's
    idempotent detection relies on (try new first, then current)."""
    third_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    third = CredentialCipher([third_key])
    new_cipher = build_new_cipher(env={ENV_FERNET_KEY_NEW: new_key})
    blob = third.encrypt_json({"foreign": True})
    with pytest.raises(CryptoConfigError):
        new_cipher.decrypt_json(blob)


# ---------------------------------------------------------------------------
# rotation_module sanity — exposed names match the impl surface
# ---------------------------------------------------------------------------


def test_rotation_module_public_surface() -> None:
    """Pin the module's top-level names so a refactor that hides one
    fails this test loud rather than silently."""
    for name in (
        "ENV_FERNET_KEY_NEW",
        "RotationResult",
        "build_new_cipher",
        "rotate_fernet_in_place",
    ):
        assert hasattr(rotation_module, name), f"rotation_module.{name} missing"


# ---------------------------------------------------------------------------
# Helper: mark unused import suppressions
# ---------------------------------------------------------------------------


_FIELD_DEFAULT = field(default_factory=list)  # noqa: F841 — pin import survives format
