"""Fernet wrapper for encrypting per-user credential payloads.

The Fernet key is read from ``ENDPOINT_FERNET_KEY`` at process startup.
Keys are base64-encoded 32-byte URL-safe values. Generate one with
``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.

Rotation is supported by passing multiple keys, newest first: the first key
encrypts; decrypt walks the list. See :class:`CredentialCipher.rotate`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = ("CredentialCipher", "CryptoConfigError", "ENV_FERNET_KEY")

ENV_FERNET_KEY = "ENDPOINT_FERNET_KEY"

_log = logging.getLogger(__name__)


class CryptoConfigError(RuntimeError):
    """Raised when the Fernet key env var is missing or malformed."""


class CredentialCipher:
    """Thin wrapper around Fernet/MultiFernet with JSON helpers."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise CryptoConfigError("at least one Fernet key is required")
        try:
            fernets = [Fernet(k.encode()) for k in keys]
        except ValueError as exc:
            # Don't interpolate exc: its repr can include the offending key bytes.
            raise CryptoConfigError("invalid Fernet key material") from exc
        self._primary = fernets[0]
        self._multi = MultiFernet(fernets)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CredentialCipher:
        source = env if env is not None else os.environ
        raw = source.get(ENV_FERNET_KEY)
        if not raw:
            raise CryptoConfigError(
                f"{ENV_FERNET_KEY} is not set; generate one with Fernet.generate_key()"
            )
        # Parse comma-separated keys without silently dropping empty entries.
        # A trailing comma or empty slot usually means the operator dropped a key
        # during rotation by mistake; fail loudly instead of discarding it.
        parts = [k.strip() for k in raw.split(",")]
        if any(part == "" for part in parts):
            raise CryptoConfigError(
                f"{ENV_FERNET_KEY} contains an empty entry; "
                "check for stray commas or missing keys during rotation"
            )
        _log.info("crypto.keys_loaded", extra={"count": len(parts)})
        return cls(parts)

    def encrypt_json(self, payload: dict[str, Any]) -> bytes:
        """Serialize ``payload`` as JSON and encrypt with the primary key."""
        return self._primary.encrypt(json.dumps(payload, separators=(",", ":")).encode())

    def decrypt_json(self, token: bytes | str) -> dict[str, Any]:
        """Decrypt ``token`` against any configured key and parse as JSON."""
        raw = token.encode() if isinstance(token, str) else token
        try:
            plaintext = self._multi.decrypt(raw)
        except InvalidToken:
            # Break the chain so stack dumps never include the ciphertext repr.
            raise CryptoConfigError("decryption failed; key may be wrong or rotated out") from None
        decoded: Any = json.loads(plaintext.decode())
        if not isinstance(decoded, dict):
            raise CryptoConfigError("decrypted payload is not a JSON object")
        return cast("dict[str, Any]", decoded)

    def rotate(self, token: bytes | str) -> bytes:
        """Re-encrypt ``token`` with the primary key (for key rotation passes)."""
        raw = token.encode() if isinstance(token, str) else token
        try:
            return self._multi.rotate(raw)
        except InvalidToken:
            raise CryptoConfigError("rotation failed; token unreadable by any key") from None
