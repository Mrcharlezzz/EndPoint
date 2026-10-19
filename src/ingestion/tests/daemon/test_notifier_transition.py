"""PostgresTokenStore.mark_expired fires the notifier only on the
valid-to-expired transition.

Postgres-backed: auto-skips when the DB isn't reachable (same gate as the
other django_db tests in this directory).
"""

from __future__ import annotations

import pytest
from ingestion.daemon.auth.notifier import LogOnlyNotifier, Notifier
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.token_store import PostgresTokenStore


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def user_expired(self, user_email: str, platform: str) -> None:
        self.calls.append((user_email, platform))


@pytest.fixture
def cipher(fernet_key: str) -> CredentialCipher:
    return CredentialCipher([fernet_key])


@pytest.mark.django_db(transaction=True)
async def test_mark_expired_fires_notifier_on_transition(cipher: CredentialCipher) -> None:
    notifier = RecordingNotifier()
    store = PostgresTokenStore(cipher, notifier=notifier)
    await store.update_credentials("alice@example.com", "teams", {"token": "v1"})

    await store.mark_expired("alice@example.com", "teams")

    assert notifier.calls == [("alice@example.com", "teams")]


@pytest.mark.django_db(transaction=True)
async def test_mark_expired_is_idempotent_against_notifier(cipher: CredentialCipher) -> None:
    notifier = RecordingNotifier()
    store = PostgresTokenStore(cipher, notifier=notifier)
    await store.update_credentials("alice@example.com", "teams", {"token": "v1"})

    await store.mark_expired("alice@example.com", "teams")
    await store.mark_expired("alice@example.com", "teams")

    # Second call finds is_expired already True — must NOT fire the notifier.
    assert notifier.calls == [("alice@example.com", "teams")]


@pytest.mark.django_db(transaction=True)
async def test_mark_expired_without_notifier_does_not_error(cipher: CredentialCipher) -> None:
    store = PostgresTokenStore(cipher)  # no notifier
    await store.update_credentials("alice@example.com", "teams", {"token": "v1"})
    await store.mark_expired("alice@example.com", "teams")  # no-op on notifier


@pytest.mark.django_db(transaction=True)
async def test_mark_expired_missing_row_does_not_fire(cipher: CredentialCipher) -> None:
    notifier = RecordingNotifier()
    store = PostgresTokenStore(cipher, notifier=notifier)
    await store.mark_expired("ghost@example.com", "teams")
    assert notifier.calls == []


@pytest.mark.django_db(transaction=True)
async def test_update_credentials_clears_expired_without_firing_notifier(
    cipher: CredentialCipher,
) -> None:
    notifier = RecordingNotifier()
    store = PostgresTokenStore(cipher, notifier=notifier)
    await store.update_credentials("alice@example.com", "teams", {"token": "v1"})
    await store.mark_expired("alice@example.com", "teams")
    assert len(notifier.calls) == 1

    # Writing fresh tokens flips is_expired back to False; no notifier call.
    await store.update_credentials("alice@example.com", "teams", {"token": "v2"})
    assert len(notifier.calls) == 1


# LogOnlyNotifier is what the daemon wires by default; exercise it so it
# doesn't bit-rot.
async def test_log_only_notifier_is_callable_and_returns_none() -> None:
    notifier: Notifier = LogOnlyNotifier()
    result = await notifier.user_expired("alice@example.com", "teams")
    assert result is None
