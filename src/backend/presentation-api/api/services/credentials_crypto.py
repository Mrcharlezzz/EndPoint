"""Fernet-over-JSON helper for PlatformCredential ``encrypted_payload``.

Format must match the daemon's ``ingestion.daemon.crypto.CredentialCipher``
so the daemon can decrypt rows we write here. Single-key for now;
rotation is a daemon-side concern.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialCryptoError(RuntimeError):
    """Raised when the Fernet key is missing or a decrypt fails."""


def _cipher() -> Fernet:
    key = settings.ENDPOINT_FERNET_KEY
    if not key:
        raise CredentialCryptoError(
            "ENDPOINT_FERNET_KEY is not set; cannot encrypt credential payloads"
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CredentialCryptoError("ENDPOINT_FERNET_KEY is malformed") from exc


def encrypt_payload(payload: dict[str, Any]) -> bytes:
    """Encrypt a JSON-serializable dict to bytes for ``encrypted_payload``."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _cipher().encrypt(raw)


def decrypt_payload(blob: bytes) -> dict[str, Any]:
    """Decrypt bytes from ``encrypted_payload`` into a dict."""
    try:
        raw = _cipher().decrypt(bytes(blob))
    except InvalidToken as exc:
        raise CredentialCryptoError("payload decryption failed") from exc
    return json.loads(raw.decode("utf-8"))
