"""PostgresTokenStore: encrypted round-trip and mark_expired semantics.

Requires the Postgres service container (``docker compose up db -d``) or a
running local Postgres. ``transaction=True`` forces per-test truncation
because Django's async ORM (``aupdate_or_create``, ``aget``) runs in a
separate thread whose savepoint doesn't always participate in the default
pytest-django rollback.
"""

from __future__ import annotations

import pytest
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.token_store import PostgresTokenStore

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def cipher(fernet_key: str) -> CredentialCipher:
    return CredentialCipher([fernet_key])


async def test_get_credentials_returns_none_when_missing(cipher: CredentialCipher) -> None:
    store = PostgresTokenStore(cipher)
    got = await store.get_credentials("ghost@example.com", "moodle")
    assert got is None


async def test_update_then_get_round_trip_decrypts_payload(cipher: CredentialCipher) -> None:
    store = PostgresTokenStore(cipher)
    await store.update_credentials(
        "alice@example.com",
        "moodle",
        {"token": "moodle-token-123", "user_id": 42},
    )
    got = await store.get_credentials("alice@example.com", "moodle")
    assert got == {"token": "moodle-token-123", "user_id": 42}


async def test_update_credentials_is_idempotent(cipher: CredentialCipher) -> None:
    store = PostgresTokenStore(cipher)
    await store.update_credentials("alice@example.com", "moodle", {"token": "v1"})
    await store.update_credentials("alice@example.com", "moodle", {"token": "v2"})
    got = await store.get_credentials("alice@example.com", "moodle")
    assert got == {"token": "v2"}


async def test_update_clears_expired_flags(cipher: CredentialCipher) -> None:
    from core.models import PlatformCredential

    store = PostgresTokenStore(cipher)
    await store.update_credentials("alice@example.com", "moodle", {"token": "v1"})
    await store.mark_expired("alice@example.com", "moodle")

    # Writing fresh creds should flip is_expired back to False.
    await store.update_credentials("alice@example.com", "moodle", {"token": "v2"})
    row = await PlatformCredential.objects.aget(
        user_email="alice@example.com",
        platform="moodle",
    )
    assert row.is_expired is False
    assert row.expired_at is None
    assert row.last_refreshed_at is not None


async def test_mark_expired_sets_flag_and_timestamp(cipher: CredentialCipher) -> None:
    from core.models import PlatformCredential

    store = PostgresTokenStore(cipher)
    await store.update_credentials("alice@example.com", "moodle", {"token": "v1"})

    await store.mark_expired("alice@example.com", "moodle")

    row = await PlatformCredential.objects.aget(
        user_email="alice@example.com",
        platform="moodle",
    )
    assert row.is_expired is True
    assert row.expired_at is not None


async def test_mark_expired_is_a_noop_for_missing_rows(cipher: CredentialCipher) -> None:
    # Not an error: update against an empty queryset simply affects 0 rows.
    store = PostgresTokenStore(cipher)
    await store.mark_expired("ghost@example.com", "moodle")
