"""Fernet wrapper round-trip, env bootstrap, and key rotation."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon.crypto import ENV_FERNET_KEY, CredentialCipher, CryptoConfigError


def test_round_trip_encrypts_and_decrypts_json() -> None:
    cipher = CredentialCipher([Fernet.generate_key().decode()])
    payload = {"access_token": "abc", "refresh_token": "def", "scopes": ["mail.read"]}

    token = cipher.encrypt_json(payload)
    decrypted = cipher.decrypt_json(token)

    assert decrypted == payload
    # Token is opaque bytes, not the plaintext.
    assert b"access_token" not in token


def test_from_env_reads_primary_key() -> None:
    key = Fernet.generate_key().decode()
    cipher = CredentialCipher.from_env({ENV_FERNET_KEY: key})

    token = cipher.encrypt_json({"x": 1})
    assert cipher.decrypt_json(token) == {"x": 1}


def test_from_env_missing_raises() -> None:
    with pytest.raises(CryptoConfigError, match=ENV_FERNET_KEY):
        CredentialCipher.from_env({})


def test_from_env_rejects_garbage_key() -> None:
    with pytest.raises(CryptoConfigError):
        CredentialCipher.from_env({ENV_FERNET_KEY: "not-a-real-fernet-key"})


def test_multi_key_decrypt_uses_fallback_then_rotates() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    old_cipher = CredentialCipher([old_key])
    rotating_cipher = CredentialCipher([new_key, old_key])

    legacy_token = old_cipher.encrypt_json({"legacy": True})
    # New cipher still decrypts tokens written with the old key.
    assert rotating_cipher.decrypt_json(legacy_token) == {"legacy": True}

    rotated = rotating_cipher.rotate(legacy_token)
    # After rotation, the old key alone can no longer read it.
    with pytest.raises(CryptoConfigError):
        old_cipher.decrypt_json(rotated)


def test_decrypt_non_object_rejected() -> None:
    # Fernet token wrapping a JSON list — valid JSON, but not a dict, so we
    # reject it to keep the credential shape contract tight.
    key = Fernet.generate_key().decode()
    fernet = Fernet(key.encode())
    token = fernet.encrypt(b"[1, 2, 3]")

    cipher = CredentialCipher([key])
    with pytest.raises(CryptoConfigError):
        cipher.decrypt_json(token)


def test_empty_key_list_rejected() -> None:
    with pytest.raises(CryptoConfigError):
        CredentialCipher([])


def test_from_env_rejects_empty_string() -> None:
    with pytest.raises(CryptoConfigError, match=ENV_FERNET_KEY):
        CredentialCipher.from_env({ENV_FERNET_KEY: ""})


def test_from_env_rejects_trailing_comma() -> None:
    # Catches the rotation footgun: "new_key," silently dropping the old key
    # would make every credential encrypted with the old key unreadable.
    key = Fernet.generate_key().decode()
    with pytest.raises(CryptoConfigError, match="empty entry"):
        CredentialCipher.from_env({ENV_FERNET_KEY: f"{key},"})


def test_from_env_rejects_bare_comma() -> None:
    with pytest.raises(CryptoConfigError, match="empty entry"):
        CredentialCipher.from_env({ENV_FERNET_KEY: ","})


def test_multi_key_decrypt_walks_full_key_list() -> None:
    # A token encrypted with the oldest key still decrypts when the cipher
    # is constructed with a longer rotation chain.
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    k3 = Fernet.generate_key().decode()

    oldest = CredentialCipher([k3])
    token = oldest.encrypt_json({"old": True})

    rotating = CredentialCipher([k1, k2, k3])
    assert rotating.decrypt_json(token) == {"old": True}
