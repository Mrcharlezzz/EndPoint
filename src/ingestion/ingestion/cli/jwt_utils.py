"""Shared JWT utilities for token inspection (no signature verification).

All functions use unverified decoding — only suitable for reading claims
like exp, iat, scp, tid. Never use for authorization decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime


def decode_unverified(token: str) -> dict | None:
    """Decode a JWT without verifying the signature.

    Returns the decoded payload dict, or None on any failure.
    """
    if not token:
        return None
    try:
        import jwt

        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None


def get_expiry_timestamp(token: str) -> int:
    """Return the ``exp`` claim as a Unix timestamp, or 0 on failure."""
    decoded = decode_unverified(token)
    if decoded is None:
        return 0
    return decoded.get("exp", 0)


def get_expiry_remaining_minutes(token: str) -> int | None:
    """Return minutes until token expires, or None if not decodable.

    Negative values mean the token is already expired.
    """
    decoded = decode_unverified(token)
    if decoded is None:
        return None
    exp = decoded.get("exp")
    if exp is None:
        return None
    expiry = datetime.fromtimestamp(exp, tz=UTC)
    remaining = expiry - datetime.now(tz=UTC)
    return int(remaining.total_seconds() / 60)


def has_scope(token: str, scope: str) -> bool:
    """Check whether a JWT's ``scp`` claim contains *scope*."""
    decoded = decode_unverified(token)
    if decoded is None:
        return False
    return scope in decoded.get("scp", "").split()


def is_expired(token: str, buffer_minutes: int = 5) -> bool:
    """Return True if the token is expired or will expire within *buffer_minutes*.

    Returns True on any decode failure (treat unknown as expired).
    """
    remaining = get_expiry_remaining_minutes(token)
    if remaining is None:
        return True
    return remaining <= buffer_minutes


def get_token_info(token: str) -> dict[str, str] | None:
    """Decode a JWT and return key display fields (Expires, Issued, Scopes, Tenant).

    Returns None if the token cannot be decoded.
    """
    decoded = decode_unverified(token)
    if decoded is None:
        return None

    info: dict[str, str] = {}

    exp = decoded.get("exp")
    if exp:
        expiry = datetime.fromtimestamp(exp, tz=UTC)
        remaining = expiry - datetime.now(tz=UTC)
        minutes = int(remaining.total_seconds() / 60)
        if minutes > 0:
            info["Expires"] = f"{expiry.isoformat()} (in {minutes}m)"
        else:
            info["Expires"] = "EXPIRED"

    iat = decoded.get("iat")
    if iat:
        info["Issued"] = datetime.fromtimestamp(iat, tz=UTC).isoformat()

    scp = decoded.get("scp", "")
    if scp:
        info["Scopes"] = scp

    tid = decoded.get("tid", "")
    if tid:
        info["Tenant"] = tid

    return info
