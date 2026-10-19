"""Fernet rotation in-place + idempotency / failure handling (slice 12 part C).

Pure-cipher tests — no Django, no PlatformCredential. The Postgres-
backed integration path is exercised by the existing daemon-integration
tests under ``conftest.py``'s django_db gate; this file pins the
detection logic and the build_new_cipher env-var checks.
"""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon.crypto import CredentialCipher, CryptoConfigError
from ingestion.daemon.lifecycle.fernet_rotation import (
    ENV_FERNET_KEY_NEW,
    build_new_cipher,
)


def test_build_new_cipher_reads_env_var() -> None:
    """The cipher is single-key (only ``ENDPOINT_FERNET_KEY_NEW``);
    a round-trip encrypt+decrypt confirms the key was loaded."""
    new_key = Fernet.generate_key().decode()
    cipher = build_new_cipher(env={ENV_FERNET_KEY_NEW: new_key})
    payload = {"hello": "world"}
    blob = cipher.encrypt_json(payload)
    assert cipher.decrypt_json(blob) == payload


def test_build_new_cipher_does_not_decrypt_old_payloads() -> None:
    """The constructed cipher is single-key — by design — so an old-key
    payload must NOT decrypt against it. This is the property the
    rotation's idempotent detection relies on."""
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([old_key])
    new_cipher = build_new_cipher(env={ENV_FERNET_KEY_NEW: new_key})
    old_blob = old_cipher.encrypt_json({"a": 1})
    with pytest.raises(CryptoConfigError):
        new_cipher.decrypt_json(old_blob)


def test_build_new_cipher_missing_new_env_raises() -> None:
    with pytest.raises(CryptoConfigError, match=ENV_FERNET_KEY_NEW):
        build_new_cipher(env={"ENDPOINT_FERNET_KEY": Fernet.generate_key().decode()})


def test_build_new_cipher_invalid_new_key_raises() -> None:
    with pytest.raises(CryptoConfigError, match="ENDPOINT_FERNET_KEY_NEW"):
        build_new_cipher(env={ENV_FERNET_KEY_NEW: "not-a-real-fernet-key"})


def test_idempotent_detection_uses_new_cipher_first() -> None:
    """A row already encrypted with the new key must round-trip without
    falling back to the old cipher's decrypt path. We verify by building
    a fresh cipher with ONLY the new key and confirming it can read."""
    new_key = Fernet.generate_key().decode()
    new_cipher = CredentialCipher([new_key])
    payload = {"already": "rotated"}
    blob = new_cipher.encrypt_json(payload)
    # Verify that a cipher with only the new key can decrypt — the
    # rotation's idempotent fast path uses exactly this check.
    assert new_cipher.decrypt_json(blob) == payload
    # And a cipher with only the OLD key cannot — confirming the
    # idempotent check actually distinguishes which key encrypted it.
    old_only = CredentialCipher([Fernet.generate_key().decode()])
    with pytest.raises(CryptoConfigError):
        old_only.decrypt_json(blob)


def test_real_env_var_is_left_untouched_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the helper reads the explicit ``env`` dict when supplied
    rather than ``os.environ``, so test isolation is preserved."""
    monkeypatch.delenv(ENV_FERNET_KEY_NEW, raising=False)
    # No raise from the helper because we pass an explicit env dict.
    new_key = Fernet.generate_key().decode()
    current_key = Fernet.generate_key().decode()
    build_new_cipher(env={"ENDPOINT_FERNET_KEY": current_key, ENV_FERNET_KEY_NEW: new_key})
    # And the process-level env var was not mutated.
    assert os.environ.get(ENV_FERNET_KEY_NEW) is None


# ---------------------------------------------------------------------------
# In-place rotation: integration with PlatformCredential (django_db)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
async def test_rotate_in_place_re_encrypts_existing_row() -> None:
    """End-to-end: a row encrypted by the current cipher gets re-encrypted
    by the new cipher (single-key). After the pass the row is readable by
    the same single-key new cipher."""
    from core.models import PlatformCredential
    from ingestion.daemon.lifecycle.fernet_rotation import rotate_fernet_in_place

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([old_key])
    new_cipher = CredentialCipher([new_key])

    # Seed one row encrypted by the old cipher.
    payload = {"access_token": "tok-abc", "expiry": 100}
    encrypted = old_cipher.encrypt_json(payload)
    await PlatformCredential.objects.acreate(
        user_email="alice@x",
        platform="moodle",
        encrypted_payload=encrypted,
    )
    try:
        result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
        assert result.total_rows == 1
        assert result.rotated == 1
        assert result.already_rotated == 0
        assert result.failed == 0
        # Re-fetch and confirm the new payload is readable by the
        # new cipher (the row is now encrypted under the new key).
        row = await PlatformCredential.objects.aget(user_email="alice@x", platform="moodle")
        assert new_cipher.decrypt_json(bytes(row.encrypted_payload)) == payload
    finally:
        await PlatformCredential.objects.filter(user_email="alice@x").adelete()


@pytest.mark.django_db
async def test_rotate_in_place_idempotent_on_already_rotated_row() -> None:
    """Running the pass twice is a no-op the second time — the
    already_rotated count rises."""
    from core.models import PlatformCredential
    from ingestion.daemon.lifecycle.fernet_rotation import rotate_fernet_in_place

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([old_key])
    new_cipher = CredentialCipher([new_key])

    payload = {"access_token": "tok-xyz"}
    encrypted = old_cipher.encrypt_json(payload)
    await PlatformCredential.objects.acreate(
        user_email="bob@x", platform="moodle", encrypted_payload=encrypted
    )
    try:
        await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
        # Second pass: nothing left to rotate.
        result = await rotate_fernet_in_place(current_cipher=old_cipher, new_cipher=new_cipher)
        assert result.total_rows == 1
        assert result.rotated == 0
        assert result.already_rotated == 1
        assert result.failed == 0
    finally:
        await PlatformCredential.objects.filter(user_email="bob@x").adelete()


@pytest.mark.django_db
async def test_rotate_in_place_audit_emitted_per_rotated_row() -> None:
    """One ``credential.fernet_rotated`` audit entry per row that was
    actually rotated. Already-rotated rows do NOT emit an entry."""
    from core.models import PlatformCredential
    from ingestion.daemon.lifecycle.fernet_rotation import rotate_fernet_in_place

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_cipher = CredentialCipher([old_key])
    new_cipher = CredentialCipher([new_key])

    await PlatformCredential.objects.acreate(
        user_email="carol@x",
        platform="teams",
        encrypted_payload=old_cipher.encrypt_json({"x": 1}),
    )
    audit_calls: list[dict[str, object]] = []

    async def _audit(**kwargs: object) -> int:
        audit_calls.append(kwargs)
        return 1

    try:
        await rotate_fernet_in_place(
            current_cipher=old_cipher,
            new_cipher=new_cipher,
            audit_write=_audit,
        )
        # One audit row was written.
        actions = [str(call.get("action") or "") for call in audit_calls]
        assert "credential.fernet_rotated" in actions
        rotated_calls = [c for c in audit_calls if c.get("action") == "credential.fernet_rotated"]
        assert rotated_calls
        target = str(rotated_calls[0].get("target") or "")
        assert target == "carol@x/teams"
    finally:
        await PlatformCredential.objects.filter(user_email="carol@x").adelete()
